"""Whole-change review gate for refactors and high-impact modifications.

This is deliberately broader than a diff summary. It identifies changed public
definitions, searches their consumers, records deleted behavior, and requires
the caller to attach verification evidence before wrap-up.
"""

from __future__ import annotations

import json
import hashlib
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List


SHARED_PATH_MARKERS = (
    "kernel/", "core/", "shared/", "common/", "base/", "public/",
    "api/", "protocol", "interface", "router", "service",
)
SOURCE_SUFFIXES = {
    ".py", ".swift", ".m", ".mm", ".h", ".c", ".cc", ".cpp",
    ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".kt", ".kts",
    ".rs", ".rb",
}
DEFINITION_RE = re.compile(
    r"^[+-]\s*(?:def|class|func|protocol|interface|public\s+(?:class|func|struct|enum))\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)
SOURCE_DEFINITION_RE = re.compile(
    r"^\s*(?:def|class|func|protocol|interface|public\s+(?:class|func|struct|enum))\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)
PUBLIC_ASSIGNMENT_RE = re.compile(
    r"^([A-Z][A-Z0-9_]*)\s*(?::[^=\n]+)?=",
    re.MULTILINE,
)
EXPORT_ASSIGNMENT_RE = re.compile(
    r"^\s*export\s+(?:const|let|var)\s+([A-Za-z_]\w*)\s*(?::[^=\n]+)?=",
    re.MULTILINE,
)
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class ImpactItem:
    kind: str
    target: str
    reason: str
    consumers: List[str] = field(default_factory=list)
    evidence_ids: List[str] = field(default_factory=list)
    status: str = "pending"  # pending | verified | accepted
    resolution: str = ""


@dataclass
class GlobalReview:
    project_root: str
    base: str
    changed_files: List[str]
    additions: int
    deletions: int
    changed_symbols: List[str]
    impacts: List[ImpactItem]
    score: int
    risk_level: str
    fingerprint: str
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "impacts": [asdict(item) for item in self.impacts],
        }

    def pending(self) -> List[ImpactItem]:
        return [item for item in self.impacts if item.status == "pending"]

    def verify(self, target: str, evidence_ids: List[str], *, accepted: bool = False,
               resolution: str = "", user_confirmation_id: str = "") -> None:
        matches = [entry for entry in self.impacts if entry.target == target]
        if not matches:
            raise KeyError(f"global-review impact not found: {target}")
        if len(matches) != 1:
            raise ValueError(f"global-review target is ambiguous: {target}")
        item = matches[0]
        if not evidence_ids and not accepted:
            raise ValueError("impact verification requires evidence ids or explicit accepted=true")
        if not resolution.strip():
            raise ValueError("impact verification requires a target-specific resolution")
        if accepted and (not resolution.strip() or not user_confirmation_id):
            raise ValueError(
                "accepted risk requires a resolution reason and user confirmation evidence"
            )
        item.evidence_ids = list(evidence_ids)
        if user_confirmation_id:
            item.evidence_ids.append(user_confirmation_id)
        item.status = "accepted" if accepted else "verified"
        item.resolution = resolution.strip()
        if not self.pending():
            self.status = "completed"
            self.completed_at = time.time()

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "GlobalReview":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["impacts"] = [ImpactItem(**row) for row in data.get("impacts", [])]
        # Old reviews remain readable but become stale and must be regenerated.
        data.setdefault("fingerprint", "")
        return cls(**data)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout


def _changed_files(root: Path, base: str) -> List[str]:
    tracked = _git(root, "diff", "--name-only", base).splitlines()
    untracked = _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({line.strip() for line in [*tracked, *untracked] if line.strip()})


def _numstat(root: Path, base: str) -> tuple[int, int]:
    additions = deletions = 0
    for line in _git(root, "diff", "--numstat", base).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            additions += int(parts[0]) if parts[0].isdigit() else 0
            deletions += int(parts[1]) if parts[1].isdigit() else 0
    for relative in _git(root, "ls-files", "--others", "--exclude-standard").splitlines():
        path = root / relative.strip()
        if path.is_file():
            try:
                additions += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                pass
    return additions, deletions


def _consumers(root: Path, symbol: str, excluded_files: set[str]) -> List[str]:
    hits = []
    token = re.compile(rf"\b{re.escape(symbol)}\b")
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        rel = str(path.relative_to(root))
        if rel.startswith((".git/", "Pods/", "DerivedData/")) or rel in excluded_files:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if token.search(text):
            hits.append(rel)
    return hits


def _changed_symbols(root: Path, base: str, changed: List[str]) -> dict[str, List[str]]:
    diff = _git(root, "diff", "--unified=0", base)
    changed_lines: dict[str, set[int]] = {}
    deleted_lines: dict[str, set[int]] = {}
    current_file = ""
    old_file = ""
    new_line = 0
    old_line = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("--- a/"):
            old_file = line[6:]
            continue
        if line.startswith("+++ b/"):
            current_file = line[6:]
            changed_lines.setdefault(current_file, set())
            in_hunk = False
            continue
        if line == "+++ /dev/null":
            current_file = old_file
            changed_lines.setdefault(current_file, set())
            in_hunk = False
            continue
        match = HUNK_RE.match(line)
        if match:
            new_line = int(match.group(1))
            old_header = re.match(r"^@@ -(\d+)", line)
            old_line = int(old_header.group(1)) if old_header else 0
            in_hunk = True
            continue
        if not in_hunk or not current_file:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            changed_lines[current_file].add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            deleted_lines.setdefault(old_file or current_file, set()).add(old_line)
            old_line += 1
        else:
            new_line += 1
            old_line += 1

    untracked = set(_git(root, "ls-files", "--others", "--exclude-standard").splitlines())
    deleted_symbols: dict[str, set[str]] = {}
    current_file = ""
    old_file = ""
    for line in diff.splitlines():
        if line.startswith("--- a/"):
            old_file = line[6:]
            continue
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        if line == "+++ /dev/null":
            current_file = old_file
            continue
        if current_file and line.startswith("-") and not line.startswith("---"):
            match = re.match(
                r"^-\s*(?:def|class|func|protocol|interface|public\s+(?:class|func|struct|enum))\s+([A-Za-z_]\w*)",
                line,
            )
            if match:
                deleted_symbols.setdefault(current_file, set()).add(match.group(1))
            assignment = re.match(r"^-([A-Z][A-Z0-9_]*)\s*(?::[^=\n]+)?=", line)
            if assignment:
                deleted_symbols.setdefault(current_file, set()).add(assignment.group(1))
            export_assignment = re.match(
                r"^-\s*export\s+(?:const|let|var)\s+([A-Za-z_]\w*)\s*(?::[^=\n]+)?=",
                line,
            )
            if export_assignment:
                deleted_symbols.setdefault(current_file, set()).add(
                    export_assignment.group(1)
                )
    symbols_by_file: dict[str, set[str]] = {}
    for relative in changed:
        path = root / relative
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        definitions = [
            (text.count("\n", 0, match.start()) + 1, match.group(1))
            for match in SOURCE_DEFINITION_RE.finditer(text)
        ]
        definitions.extend(
            (text.count("\n", 0, match.start()) + 1, match.group(1))
            for match in PUBLIC_ASSIGNMENT_RE.finditer(text)
        )
        definitions.extend(
            (text.count("\n", 0, match.start()) + 1, match.group(1))
            for match in EXPORT_ASSIGNMENT_RE.finditer(text)
        )
        definitions.sort()
        if relative in untracked:
            symbols_by_file.setdefault(relative, set()).update(
                name for _, name in definitions if not name.startswith("_")
            )
            continue
        for line_number in changed_lines.get(relative, set()):
            preceding = [entry for entry in definitions if entry[0] <= line_number]
            if preceding:
                symbols_by_file.setdefault(relative, set()).add(preceding[-1][1])
    for relative, names in deleted_symbols.items():
        symbols_by_file.setdefault(relative, set()).update(names)
    for relative, lines in deleted_lines.items():
        if not lines:
            continue
        try:
            old_text = _git(root, "show", f"{base}:{relative}")
        except RuntimeError:
            continue
        old_definitions = [
            (old_text.count("\n", 0, match.start()) + 1, match.group(1))
            for match in SOURCE_DEFINITION_RE.finditer(old_text)
        ]
        old_definitions.extend(
            (old_text.count("\n", 0, match.start()) + 1, match.group(1))
            for match in PUBLIC_ASSIGNMENT_RE.finditer(old_text)
        )
        old_definitions.extend(
            (old_text.count("\n", 0, match.start()) + 1, match.group(1))
            for match in EXPORT_ASSIGNMENT_RE.finditer(old_text)
        )
        old_definitions.sort()
        for line_number in lines:
            preceding = [entry for entry in old_definitions if entry[0] <= line_number]
            if preceding:
                symbols_by_file.setdefault(relative, set()).add(preceding[-1][1])
    return {path: sorted(names) for path, names in symbols_by_file.items() if names}


def analyze_global_impact(project_root: str | Path, *, base: str = "HEAD") -> GlobalReview:
    root = Path(project_root).resolve()
    changed = _changed_files(root, base)
    additions, deletions = _numstat(root, base)
    symbols_by_file = _changed_symbols(root, base, changed)
    symbols = sorted({name for names in symbols_by_file.values() for name in names})
    impacts: List[ImpactItem] = []
    for relative, file_symbols in sorted(symbols_by_file.items()):
        consumers = sorted({
            consumer
            for symbol in file_symbols
            for consumer in _consumers(root, symbol, {relative})
        })
        impacts.append(ImpactItem(
            kind="changed_surface",
            target=relative,
            reason="改动定义或行为：" + ", ".join(file_symbols),
            consumers=consumers,
        ))
    # A changed consumer can alter how an unchanged public surface is used.
    for relative in changed:
        if relative in symbols_by_file:
            continue
        path = root / relative
        if path.suffix not in SOURCE_SUFFIXES:
            continue
        if not path.is_file():
            impacts.append(ImpactItem(
                kind="deleted_source",
                target=relative,
                reason="源码文件被删除，需要确认其顶层副作用、入口和替代路径",
            ))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        referenced = []
        for symbol in symbols:
            if re.search(rf"\b{re.escape(symbol)}\b", text):
                referenced.append(symbol)
        impacts.append(ImpactItem(
            kind="changed_consumer",
            target=relative,
            reason=(
                "调用关系发生变化，涉及：" + ", ".join(sorted(referenced))
                if referenced
                else "源文件行为发生变化，需要确认其入口和下游影响"
            ),
        ))
    represented = {item.target for item in impacts}
    for relative in changed:
        if Path(relative).suffix not in SOURCE_SUFFIXES or relative in represented:
            continue
        impacts.append(ImpactItem(
            kind="changed_source",
            target=relative,
            reason="源码文件发生变化，需要逐文件确认入口、下游和行为影响",
        ))
    for path in changed:
        low = path.lower()
        if (
            any(marker in low for marker in SHARED_PATH_MARKERS)
            and path not in {item.target for item in impacts}
        ):
            impacts.append(ImpactItem(
                kind="shared_surface",
                target=path,
                reason="改动位于共享/公共边界，需要验证所有主要使用路径",
            ))
    if deletions:
        impacts.append(ImpactItem(
            kind="deleted_behavior",
            target="deleted-lines",
            reason=f"删除 {deletions} 行，必须说明原逻辑服务对象及替代路径",
        ))
    if not impacts:
        impacts.append(ImpactItem(
            kind="diff_scope",
            target="changed-files",
            reason="确认完整 diff 与任务目标一致，且无意外文件",
        ))

    score = additions // 40 + deletions // 20 + len(changed) * 3 + len(symbols) * 2
    if any(item.kind == "shared_surface" for item in impacts):
        score += 10
    risk = "high" if score >= 30 else "unsure" if score >= 10 else "low"
    untracked_hashes = {}
    for relative in _git(root, "ls-files", "--others", "--exclude-standard").splitlines():
        path = root / relative.strip()
        if path.is_file():
            untracked_hashes[relative.strip()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return GlobalReview(
        project_root=str(root),
        base=base,
        changed_files=changed,
        additions=additions,
        deletions=deletions,
        changed_symbols=symbols,
        impacts=impacts,
        score=score,
        risk_level=risk,
        fingerprint=hashlib.sha256(
            json.dumps({
                "changed": changed,
                "additions": additions,
                "deletions": deletions,
                "symbols": symbols_by_file,
                "diff": _git(root, "diff", "--binary", base),
                "untracked_hashes": untracked_hashes,
                "impact_scope": [
                    {
                        "kind": item.kind,
                        "target": item.target,
                        "reason": item.reason,
                        "consumers": item.consumers,
                    }
                    for item in impacts
                ],
            }, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    )
