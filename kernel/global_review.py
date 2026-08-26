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
from typing import List, Optional


# 评审三裁决协议：对一片改动只给三种结论之一，对照计划期冻结的设计契约（不改边界），
# 而不是对照"最新一句话"。忠实对齐内部 VDD 设计基线层。
THREE_VERDICT_PROTOCOL = (
    "评审三裁决（对照设计契约逐条判，禁开放式找茬）："
    "①符合基线→明说\"符合，不改\"（合法交付，别硬挤问题）"
    "②偏离基线→列偏离点+文件行号+改回（唯一动代码情形）"
    "③改动合理但基线没覆盖/基线该改→停手，显式确认后先改基线再改代码，"
    "禁止代码先跑偏事后追认。映射不到任何基线条款的\"问题\"记为基线缺口另议，不当 bug 顺手扩改。"
)

# 设计契约的字段（计划期冻结、可留空的软基线；不是不可绕过的硬 Gate）。
DESIGN_CONTRACT_FIELDS = ("objectives", "design_decisions", "non_goals")


def design_contract_filled(contract: dict | None) -> bool:
    """契约是否被真正填写：任一实质字段有非空内容才算，纯空壳视为留空。"""
    if not isinstance(contract, dict):
        return False
    for key in DESIGN_CONTRACT_FIELDS:
        value = contract.get(key)
        if isinstance(value, (list, tuple)):
            if any(str(item).strip() for item in value):
                return True
        elif str(value or "").strip():
            return True
    return False


SHARED_PATH_MARKERS = (
    "kernel/", "core/", "shared/", "common/", "base/", "public/",
    "api/", "protocol", "interface", "router", "service",
)
SOURCE_SUFFIXES = {
    ".py", ".swift", ".m", ".mm", ".h", ".c", ".cc", ".cpp",
    ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".kt", ".kts",
    ".rs", ".rb",
}
BEHAVIOR_SUFFIXES = {
    ".json", ".yaml", ".yml", ".plist", ".podspec", ".sh", ".bash",
    ".xcconfig", ".entitlements", ".toml", ".ini", ".xml", ".pbxproj",
    ".resolved", ".lock", ".gradle", ".properties", ".strings",
    ".stringsdict", ".storyboard", ".xib", ".bazel", ".css", ".scss",
    ".sass", ".less", ".html", ".vue", ".svelte",
}
BEHAVIOR_FILENAMES = {
    "Dockerfile", "Makefile", "Podfile", "Gemfile", "BUILD", "WORKSPACE",
    "CMakeLists.txt",
}
DEFINITION_RE = re.compile(
    r"^[+-]\s*(?:def|class|func|protocol|interface|public\s+(?:class|func|struct|enum))\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)
SOURCE_DEFINITION_RE = re.compile(
    r"^\s*(?:(?:@\w+(?:\([^)]*\))?|public|private|internal|open|final|"
    r"static|override|dynamic|mutating|nonmutating)\s+)*"
    r"(?:def|class|func|protocol|interface|struct|enum|extension)\s+"
    r"([A-Za-z_]\w*)",
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
OBJC_METHOD_RE = re.compile(
    r"^\s*[+-]\s*\([^)]*\)\s*([^;{]+)(?=[;{])",
    re.MULTILINE,
)
DYNAMIC_ENTRY_RE = re.compile(
    r"(?:(?:"
    r"(?:register\w*|openurl|subscribe|postnotification|eventname|"
    r"jsb\w*|bridge\w*)\s*(?:\(\s*|:\s*)@?"
    r"|[\"']?(?:route|router|scheme|service|protocol|notification|eventname)"
    r"[\"']?\s*:\s*"
    r")[\"']([^\"'\n]{2,160})[\"']"
    r"|[\"']?(?:route|router|scheme|service|protocol|notification|eventname)"
    r"[\"']?\s*:\s*([^\s#,\]}]{2,160}))",
    re.IGNORECASE,
)


@dataclass
class ImpactItem:
    kind: str
    target: str
    reason: str
    consumers: List[str] = field(default_factory=list)
    entry_points: List[str] = field(default_factory=list)
    suggested_tests: List[str] = field(default_factory=list)
    ownership: str = "unknown"
    verification_scope: str = "R2"
    verification_mode: str = "targeted"
    visual_required: bool = False
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
    verification_scope: str = "R0"
    verification_mode: str = "spot"
    visual_required: bool = False
    symptom_is_ui: bool = False
    scope_rules: dict = field(default_factory=dict)
    unknown_impacts: List[str] = field(default_factory=list)
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

    def verify(
        self,
        target: str,
        evidence_ids: List[str],
        *,
        accepted: bool = False,
        resolution: str = "",
        user_confirmation_id: str = "",
        evidence_capabilities: Optional[List[str]] = None,
        evidence_subjects: Optional[dict[str, List[str]]] = None,
    ) -> None:
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
        if item.visual_required:
            screenshot_ids = [
                evidence_id
                for evidence_id, capability in zip(
                    evidence_ids,
                    evidence_capabilities or [],
                )
                if capability == "screenshot"
            ]
            subjects = evidence_subjects or {}
            if not any(
                item.target in subjects.get(evidence_id, [])
                for evidence_id in screenshot_ids
            ):
                raise ValueError(
                    "visual impact verification requires screenshot "
                    "evidence covering the target"
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
        data["impacts"] = [
            ImpactItem(**row) for row in data.get("impacts", [])
        ]
        # Old reviews remain readable but become stale and must be regenerated.
        data.setdefault("fingerprint", "")
        data.setdefault("verification_scope", "R0")
        data.setdefault("verification_mode", "spot")
        data.setdefault("visual_required", False)
        data.setdefault("symptom_is_ui", False)
        data.setdefault("scope_rules", {})
        data.setdefault("unknown_impacts", [])
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


def _objc_message_has_selector(text: str, selector_parts: List[str]) -> bool:
    messages: List[List[str]] = []
    for char in text:
        if char == "[":
            if messages:
                messages[-1].append(" ")
            messages.append([])
            continue
        if char == "]":
            if not messages:
                continue
            message = "".join(messages.pop())
            position = 0
            for part in selector_parts:
                match = re.search(
                    rf"\b{re.escape(part)}\s*:",
                    message[position:],
                )
                if not match:
                    break
                position += match.end()
            else:
                return True
            continue
        if messages:
            messages[-1].append(char)
    return False


def _consumers(
    root: Path,
    symbol: str,
    excluded_files: set[str],
    definition_file: str,
) -> List[str]:
    hits = []
    selector_parts = [part for part in symbol.split(":") if part]
    token = re.compile(rf"\b{re.escape(symbol)}\b") if symbol.isidentifier() else None
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        rel = str(path.relative_to(root))
        if (
            rel.startswith((".git/", "Pods/", "DerivedData/"))
            or rel in excluded_files
            or _is_test_path(rel)
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        search_text = (
            OBJC_METHOD_RE.sub("", text)
            if Path(definition_file).suffix in {".m", ".mm", ".h"}
            and path.suffix in {".m", ".mm", ".h"}
            else text
        )
        selector_match = False
        if ":" in symbol and selector_parts:
            selector_match = _objc_message_has_selector(
                search_text,
                selector_parts,
            )
        matched = (
            bool(token and token.search(search_text))
            or selector_match
            or (token is None and ":" not in symbol and symbol in search_text)
        )
        definition_suffix = Path(definition_file).suffix
        if matched and definition_suffix == ".py" and path.suffix == ".py":
            matched = bool(re.search(
                rf"^\s*(?:from\s+[\w.]+\s+import\s+[^\n]*\b{re.escape(symbol)}\b"
                rf"|import\s+[^\n]*\b{re.escape(Path(definition_file).stem)}\b)",
                text,
                re.MULTILINE,
            ))
        if (
            matched
            and definition_suffix in {".js", ".jsx", ".ts", ".tsx"}
            and path.suffix in {".js", ".jsx", ".ts", ".tsx"}
        ):
            matched = bool(re.search(
                rf"^\s*(?:import|require)[^\n]*\b{re.escape(symbol)}\b",
                text,
                re.MULTILINE,
            ))
        if matched:
            hits.append(rel)
    return hits


def _objc_selectors(text: str) -> List[tuple[int, str]]:
    selectors = []
    for match in OBJC_METHOD_RE.finditer(text):
        declaration = match.group(1)
        parts = re.findall(r"([A-Za-z_]\w*)\s*:", declaration)
        selector = "".join(f"{part}:" for part in parts)
        if not selector:
            plain = re.match(r"([A-Za-z_]\w*)", declaration.strip())
            selector = plain.group(1) if plain else ""
        if selector:
            selectors.append(
                (text.count("\n", 0, match.start()) + 1, selector)
            )
    return selectors


def _definitions(text: str, suffix: str = "") -> List[tuple[int, str]]:
    if suffix == ".swift":
        definitions = _swift_definitions(text)
    else:
        definitions = [
            (text.count("\n", 0, match.start()) + 1, match.group(1))
            for match in SOURCE_DEFINITION_RE.finditer(text)
        ]
        if suffix in {".m", ".mm", ".h"}:
            definitions.extend(_objc_selectors(text))
    definitions.extend(
        (text.count("\n", 0, match.start()) + 1, match.group(1))
        for match in PUBLIC_ASSIGNMENT_RE.finditer(text)
    )
    definitions.extend(
        (text.count("\n", 0, match.start()) + 1, match.group(1))
        for match in EXPORT_ASSIGNMENT_RE.finditer(text)
    )
    return sorted(set(definitions))


def _swift_structure_line(line: str, state: dict) -> str:
    output = []
    index = 0
    in_string = False
    while index < len(line):
        if state["block_comment_depth"]:
            if line.startswith("/*", index):
                state["block_comment_depth"] += 1
                index += 2
                continue
            if line.startswith("*/", index):
                state["block_comment_depth"] -= 1
                index += 2
                continue
            index += 1
            continue
        if state["triple_string"]:
            end = line.find('"""', index)
            if end < 0:
                return "".join(output)
            state["triple_string"] = False
            index = end + 3
            continue
        if not in_string and line.startswith("//", index):
            break
        if not in_string and line.startswith("/*", index):
            state["block_comment_depth"] = 1
            index += 2
            continue
        if not in_string and line.startswith('"""', index):
            state["triple_string"] = True
            index += 3
            continue
        char = line[index]
        if char == '"' and (index == 0 or line[index - 1] != "\\"):
            in_string = not in_string
            index += 1
            continue
        if not in_string:
            output.append(char)
        index += 1
    return "".join(output)


def _swift_structure_text(text: str) -> str:
    state = {"block_comment_depth": 0, "triple_string": False}
    return "\n".join(
        _swift_structure_line(line, state)
        for line in text.splitlines()
    )


def _swift_definitions(text: str) -> List[tuple[int, str]]:
    definitions = []
    property_pattern = re.compile(
        r"^\s*(?:(?:@\w+(?:\([^)]*\))?|public|open|final|"
        r"(?:private|fileprivate|internal|package)(?:\s*\(\s*set\s*\))?|"
        r"static|override|dynamic|lazy|weak|unowned)\s+)*"
        r"(?:var|let)\s+([A-Za-z_]\w*)"
    )
    type_pattern = re.compile(
        r"^\s*(?:(?:@\w+(?:\([^)]*\))?|public|private|internal|open|final|"
        r"fileprivate|package|static)\s+)*"
        r"(?:class|struct|enum|extension|protocol|actor)\s+([A-Za-z_]\w*)"
    )
    function_pattern = re.compile(
        r"^\s*(?:(?:@\w+(?:\([^)]*\))?|public|private|internal|open|final|"
        r"fileprivate|package|static|override|dynamic|mutating|nonmutating)\s+)*"
        r"func\s+([A-Za-z_]\w*)"
    )
    type_scope = re.compile(
        r"\b(?:class|struct|enum|extension|protocol|actor)\s+[A-Za-z_]\w*"
    )
    function_scope = re.compile(
        r"\b(?:func|init|deinit|subscript)\b"
    )
    scopes: List[str] = []
    pending_scope = ""
    lexical_state = {"block_comment_depth": 0, "triple_string": False}
    for line_number, line in enumerate(text.splitlines(), 1):
        code_line = _swift_structure_line(line, lexical_state)
        stripped = code_line.lstrip()
        leading_closes = len(stripped) - len(stripped.lstrip("}"))
        for _ in range(min(leading_closes, len(scopes))):
            scopes.pop()
        is_local = any(scope in {"function", "other"} for scope in scopes)
        for pattern in (type_pattern, function_pattern, property_pattern):
            match = pattern.match(code_line)
            if match and not is_local:
                definitions.append((line_number, match.group(1)))
                break
        opens = code_line.count("{")
        closes = max(0, code_line.count("}") - leading_closes)
        declaration_scope = (
            "type" if type_scope.search(code_line)
            else "function" if function_scope.search(code_line)
            else ""
        )
        first_scope = declaration_scope or pending_scope or "other"
        if opens:
            pending_scope = ""
        elif declaration_scope:
            pending_scope = declaration_scope
        for index in range(opens):
            scopes.append(first_scope if index == 0 else "other")
        for _ in range(min(closes, len(scopes))):
            scopes.pop()
    return definitions


def _yaml_mapping_entries(text: str) -> List[str]:
    entries = []
    container_indent = None
    container = re.compile(
        r"^(\s*)(?:routes?|routers?|schemes?|services?|protocols?|"
        r"notifications?|eventnames?)\s*:\s*(?:#.*)?$",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        match = container.match(line)
        if match:
            container_indent = len(match.group(1))
            continue
        if container_indent is None or not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= container_indent:
            container_indent = None
            continue
        key_match = re.match(r"^\s*(.+):\s+\S", line)
        if key_match:
            key = key_match.group(1).strip().strip("\"'")
            if len(key) >= 2:
                entries.append(key)
    return entries


def _entry_points(text: str, suffix: str = "") -> List[str]:
    entries = {
        (match.group(1) or match.group(2)).strip()
        for match in DYNAMIC_ENTRY_RE.finditer(text)
        if match.group(1) or match.group(2)
    }
    if suffix in {".yaml", ".yml"}:
        entries.update(_yaml_mapping_entries(text))
    return sorted(entries)


def _is_test_path(path: str | Path) -> bool:
    relative = str(path).replace("\\", "/")
    name = Path(relative).name.lower()
    parts = {part.lower() for part in Path(relative).parts}
    return (
        bool(parts & {
            "test", "tests", "fixture", "fixtures", "testfixtures",
            "mock", "mocks", "stub", "stubs", "fake", "fakes",
            "snapshot", "snapshots",
        })
        or name.startswith("test_")
        or "_test." in name
        or name.endswith("tests.swift")
        or "fixture" in name
        or "mock" in name
        or name in {"selftest.py", "selftest_ios.py"}
    )


def _literal_consumers(
    root: Path,
    token: str,
    excluded_files: set[str],
) -> List[str]:
    hits = []
    relevant_suffixes = SOURCE_SUFFIXES | BEHAVIOR_SUFFIXES
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in relevant_suffixes and path.name not in BEHAVIOR_FILENAMES:
            continue
        relative = str(path.relative_to(root))
        if (
            relative.startswith((".git/", "Pods/", "DerivedData/"))
            or relative in excluded_files
            or _is_test_path(relative)
        ):
            continue
        try:
            if token in path.read_text(encoding="utf-8", errors="replace"):
                hits.append(relative)
        except OSError:
            continue
    return hits


def _suggested_tests(
    root: Path,
    target: str,
    tokens: List[str],
    consumers: List[str],
) -> List[str]:
    candidates = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if not _is_test_path(path.relative_to(root)):
            continue
        if path.suffix not in SOURCE_SUFFIXES:
            continue
        candidates.append(path)
    direct_needles = {
        Path(target).stem,
        *tokens,
    }
    consumer_needles = {Path(item).stem for item in consumers}

    def contains(text: str, needle: str) -> bool:
        if not needle:
            return False
        if needle.isidentifier():
            return bool(re.search(rf"\b{re.escape(needle)}\b", text))
        return needle in text

    def select(needles: set[str]) -> List[str]:
        selected = []
        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            haystack = f"{path.relative_to(root)}\n{text}"
            if any(contains(haystack, needle) for needle in needles):
                selected.append(str(path.relative_to(root)))
        return selected

    selected = select(direct_needles)
    if not selected:
        selected = select(consumer_needles)
    return sorted(selected) or ["<full-suite>"]


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
        definitions = _definitions(text, path.suffix)
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
        if Path(relative).suffix == ".swift":
            continue
        symbols_by_file.setdefault(relative, set()).update(names)
    for relative, lines in deleted_lines.items():
        if not lines:
            continue
        try:
            old_text = _git(root, "show", f"{base}:{relative}")
        except RuntimeError:
            continue
        old_definitions = _definitions(old_text, Path(relative).suffix)
        for line_number in lines:
            preceding = [entry for entry in old_definitions if entry[0] <= line_number]
            if preceding:
                symbols_by_file.setdefault(relative, set()).add(preceding[-1][1])
    return {path: sorted(names) for path, names in symbols_by_file.items() if names}


_SCOPE_ORDER = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}


def _path_matches(path: str, patterns: List[str]) -> bool:
    lowered = path.lower()
    return any(
        pattern
        and (
            lowered == pattern.lower()
            or lowered.startswith(pattern.lower())
            or pattern.lower() in lowered
        )
        for pattern in patterns
    )


def _is_visual_path(path: str) -> bool:
    candidate = Path(path)
    suffix = candidate.suffix.lower()
    if suffix in {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
        ".xcassets", ".storyboard", ".xib", ".tsx", ".jsx",
        ".css", ".scss", ".sass", ".less", ".html", ".vue", ".svelte",
    }:
        return True
    parts = {part.casefold() for part in candidate.parts[:-1]}
    if (
        suffix in SOURCE_SUFFIXES | BEHAVIOR_SUFFIXES
        and parts & {
        "ui", "view", "views", "screen", "screens", "page", "pages",
        "component", "components",
        }
    ):
        return True
    if suffix not in {".swift", ".m", ".mm", ".h"}:
        return False
    return bool(re.search(
        r"(?:view|viewcontroller|screen|page|cell|header|footer)$",
        candidate.stem,
        re.IGNORECASE,
    ))


def _normalize_scope_rules(rules: Optional[dict]) -> dict:
    if rules is None:
        return {}
    if not isinstance(rules, dict):
        raise ValueError("verification scope rules must be an object")
    supported = {
        "global_shared", "module_shared", "ui_hint",
        "asset_hint", "ignore",
    }
    unknown = sorted(set(rules) - supported)
    if unknown:
        raise ValueError(
            "unknown verification scope rules: "
            + ", ".join(unknown)
        )
    normalized = {}
    for key, value in rules.items():
        if (
            not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
        ):
            raise ValueError(
                f"verification scope rule '{key}' must be an array of strings"
            )
        normalized[key] = [
            item.strip() for item in value if item.strip()
        ]
    return normalized


def _verification_scope(
    item: ImpactItem,
    *,
    changed_files: List[str],
    symptom_is_ui: bool,
    rules: dict,
) -> tuple[str, str, str, bool]:
    target = item.target
    ownership = "unknown"
    scope = "R2"
    visual = symptom_is_ui or _is_visual_path(target)
    if item.kind in {"shared_surface", "deleted_behavior"}:
        ownership, scope = "global_shared", "R3"
    elif item.kind == "behavioral_file" and item.entry_points:
        ownership, scope = "global_shared", "R3"
    elif any(
        marker in target.lower() for marker in SHARED_PATH_MARKERS
    ):
        ownership, scope = "global_shared", "R3"
    elif item.consumers:
        roots = {
            Path(path).parts[0]
            for path in [target, *item.consumers]
            if Path(path).parts
        }
        if len(roots) > 1:
            ownership, scope = "global_shared", "R3"
        else:
            ownership, scope = "module_shared", "R2"
    else:
        suffix = Path(target).suffix.lower()
        if suffix in {
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
            ".xcassets", ".strings",
        }:
            ownership, scope, visual = "page_private", "R0", True
        elif suffix in SOURCE_SUFFIXES or suffix in BEHAVIOR_SUFFIXES:
            ownership, scope = "unknown", "R2"
        elif changed_files and all(
            Path(path).suffix.lower() in {".md", ".txt", ".rst"}
            for path in changed_files
        ):
            ownership, scope = "documentation", "R0"

    # Project rules may conservatively increase scope, never lower the
    # code-derived floor.
    if _path_matches(target, list(rules.get("global_shared", []))):
        ownership, scope = "global_shared", "R3"
    elif (
        _path_matches(target, list(rules.get("module_shared", [])))
        and _SCOPE_ORDER[scope] < _SCOPE_ORDER["R2"]
    ):
        ownership, scope = "module_shared", "R2"
    if _path_matches(target, list(rules.get("ui_hint", []))):
        visual = True
        if _SCOPE_ORDER[scope] < _SCOPE_ORDER["R1"]:
            ownership, scope = "page_private", "R1"
    if _path_matches(target, list(rules.get("asset_hint", []))):
        visual = True
    if (
        scope == "R0"
        and ownership == "documentation"
        and _path_matches(target, list(rules.get("ignore", [])))
    ):
        ownership = "ignored"
    if visual and _SCOPE_ORDER[scope] < _SCOPE_ORDER["R1"]:
        scope = "R1"
    mode = {
        "R0": "spot",
        "R1": "spot",
        "R2": "targeted",
        "R3": "full",
    }[scope]
    return ownership, scope, mode, visual


def analyze_global_impact(
    project_root: str | Path,
    *,
    base: str = "HEAD",
    symptom_is_ui: bool = False,
    scope_rules: Optional[dict] = None,
) -> GlobalReview:
    root = Path(project_root).resolve()
    normalized_rules = _normalize_scope_rules(scope_rules)
    changed = _changed_files(root, base)
    additions, deletions = _numstat(root, base)
    symbols_by_file = _changed_symbols(root, base, changed)
    symbols = sorted({name for names in symbols_by_file.values() for name in names})
    entry_points_by_file = {}
    for relative in changed:
        path = root / relative
        texts = []
        if path.is_file():
            try:
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        try:
            texts.append(_git(root, "show", f"{base}:{relative}"))
        except RuntimeError:
            pass
        entries = (
            []
            if _is_test_path(relative)
            else sorted({
                entry
                for text in texts
                for entry in _entry_points(text, path.suffix)
            })
        )
        if entries:
            entry_points_by_file[relative] = entries
    impacts: List[ImpactItem] = []
    for relative, file_symbols in sorted(symbols_by_file.items()):
        consumers = sorted({
            consumer
            for symbol in file_symbols
            for consumer in _consumers(root, symbol, {relative}, relative)
        })
        entries = entry_points_by_file.get(relative, [])
        consumers = sorted({
            *consumers,
            *(
                consumer
                for entry in entries
                for consumer in _literal_consumers(root, entry, {relative})
            ),
        })
        impacts.append(ImpactItem(
            kind="changed_surface",
            target=relative,
            reason=(
                "改动定义或行为：" + ", ".join(file_symbols)
                + (f"；动态入口：{', '.join(entries)}" if entries else "")
            ),
            consumers=consumers,
            entry_points=entries,
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
                entry_points=entry_points_by_file.get(relative, []),
            ))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        referenced = []
        for symbol in symbols:
            if re.search(rf"\b{re.escape(symbol)}\b", text):
                referenced.append(symbol)
        entries = entry_points_by_file.get(relative, [])
        consumers = sorted({
            consumer
            for entry in entries
            for consumer in _literal_consumers(root, entry, {relative})
        })
        impacts.append(ImpactItem(
            kind="changed_consumer",
            target=relative,
            reason=(
                "调用关系发生变化，涉及：" + ", ".join(sorted(referenced))
                if referenced
                else "源文件行为发生变化，需要确认其入口和下游影响"
            ),
            consumers=consumers,
            entry_points=entries,
        ))
    represented = {item.target for item in impacts}
    for relative in changed:
        path = root / relative
        is_behavior = (
            path.suffix in BEHAVIOR_SUFFIXES
            or path.name in BEHAVIOR_FILENAMES
        )
        if not is_behavior or relative in represented:
            continue
        entries = entry_points_by_file.get(relative, [])
        consumers = sorted({
            consumer
            for entry in entries
            for consumer in _literal_consumers(root, entry, {relative})
        })
        impacts.append(ImpactItem(
            kind="behavioral_file",
            target=relative,
            reason=(
                "路由/构建/配置等行为文件发生变化"
                + (f"；动态入口：{', '.join(entries)}" if entries else "")
            ),
            consumers=consumers,
            entry_points=entries,
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
    if changed and not impacts:
        impacts.append(ImpactItem(
            kind="diff_scope",
            target="changed-files",
            reason="确认完整 diff 与任务目标一致，且无意外文件",
        ))
    for item in impacts:
        tokens = [
            *symbols_by_file.get(item.target, []),
            *item.entry_points,
        ]
        item.suggested_tests = _suggested_tests(
            root,
            item.target,
            tokens,
            item.consumers,
        )
        (
            item.ownership,
            item.verification_scope,
            item.verification_mode,
            item.visual_required,
        ) = _verification_scope(
            item,
            changed_files=changed,
            symptom_is_ui=symptom_is_ui,
            rules=normalized_rules,
        )

    verification_scope = max(
        (item.verification_scope for item in impacts),
        key=lambda value: _SCOPE_ORDER[value],
        default="R0",
    )
    verification_mode = {
        "R0": "spot",
        "R1": "spot",
        "R2": "targeted",
        "R3": "full",
    }[verification_scope]
    visual_required = symptom_is_ui or any(
        item.visual_required for item in impacts
    )
    if visual_required and verification_scope == "R0":
        verification_scope = "R1"
        verification_mode = "spot"
    unknown_impacts = sorted(
        item.target for item in impacts
        if item.ownership == "unknown"
    )

    score = additions // 40 + deletions // 20 + len(changed) * 3 + len(symbols) * 2
    if any(item.kind == "shared_surface" for item in impacts):
        score += 10
    risk = "high" if score >= 30 else "unsure" if score >= 10 else "low"
    untracked_hashes = {}
    for relative in _git(root, "ls-files", "--others", "--exclude-standard").splitlines():
        path = root / relative.strip()
        if path.is_file():
            untracked_hashes[relative.strip()] = hashlib.sha256(path.read_bytes()).hexdigest()
    no_changes = not changed
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
                "symptom_is_ui": bool(symptom_is_ui),
                "scope_rules": normalized_rules,
                "impact_scope": [
                    {
                        "kind": item.kind,
                        "target": item.target,
                        "reason": item.reason,
                        "consumers": item.consumers,
                        "entry_points": item.entry_points,
                        "suggested_tests": item.suggested_tests,
                        "ownership": item.ownership,
                        "verification_scope": item.verification_scope,
                        "verification_mode": item.verification_mode,
                        "visual_required": item.visual_required,
                    }
                    for item in impacts
                ],
            }, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        verification_scope=verification_scope,
        verification_mode=verification_mode,
        visual_required=visual_required,
        symptom_is_ui=bool(symptom_is_ui),
        scope_rules=normalized_rules,
        unknown_impacts=unknown_impacts,
        status="completed" if no_changes else "pending",
        completed_at=time.time() if no_changes else 0.0,
    )
