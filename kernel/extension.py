"""扩展机制 —— 二次开发的唯一入口，核心只读硬边界。

任何业务二开只改扩展目录（~/.iloop/extensions/<team.ext>/），核心整体只读。
extension_validate 检查：越界改核心、flow 命名空间冲突、manifest 合法性。

对应内部版 EXTENDING.md + extension_manager.py 的开源精简实现。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .flow import Flow, FlowRegistry

MANIFEST_NAME = "manifest.json"
FLOWS_NAME = "flows.json"


@dataclass
class ValidationIssue:
    level: str   # error | warning
    message: str


@dataclass
class Extension:
    name: str            # 命名空间，如 "team.oncall"
    root: Path
    manifest: dict = field(default_factory=dict)

    @property
    def namespace(self) -> str:
        return self.name


def scaffold_extension(name: str, base_dir: str | Path) -> Extension:
    """创建一个扩展包骨架。name 必须是 <team>.<ext> 形式做命名空间。"""
    if not re.match(r"^[a-z0-9_]+\.[a-z0-9_]+$", name):
        raise ValueError(f"扩展名必须是 '<team>.<ext>' 形式（小写/数字/下划线），得到: {name}")
    root = Path(base_dir) / name
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": "0.0.1",
        "iloop_kernel": ">=0.0.1",
        "description": f"{name} iLoop extension",
        "provides": {"flows": FLOWS_NAME, "plugin": None},
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # flow 必须带命名空间前缀，防覆盖核心
    (root / FLOWS_NAME).write_text(json.dumps([{
        "flow_id": f"{name}.example",
        "name": f"{name} 示例 flow",
        "autonomy": "L1",
        "when_keywords": ["示例"],
        "guidance": "在此实现你的业务 flow；flow_id 必须以扩展名为前缀。",
        "required_docs": [],
        "evidence_strategy": "按你的领域定",
        "escalate_when": "命中你定义的卡口"
    }], ensure_ascii=False, indent=2), encoding="utf-8")
    return Extension(name=name, root=root, manifest=manifest)


def load_extension(root: str | Path) -> Extension:
    root = Path(root)
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    return Extension(name=manifest["name"], root=root, manifest=manifest)


def validate_extension(ext: Extension, *, core_flow_ids: Optional[set[str]] = None) -> List[ValidationIssue]:
    """校验扩展包：命名空间、flow 前缀、不与核心 flow_id 冲突。"""
    issues: List[ValidationIssue] = []
    core_flow_ids = core_flow_ids or set()

    if not re.match(r"^[a-z0-9_]+\.[a-z0-9_]+$", ext.name):
        issues.append(ValidationIssue("error", f"扩展名非法: {ext.name}"))

    flows_file = ext.root / FLOWS_NAME
    if flows_file.exists():
        try:
            flows = json.loads(flows_file.read_text(encoding="utf-8"))
        except Exception as e:
            issues.append(ValidationIssue("error", f"flows.json 解析失败: {e}"))
            flows = []
        for f in flows:
            fid = f.get("flow_id", "")
            if not fid.startswith(ext.name + "."):
                issues.append(ValidationIssue("error",
                    f"flow_id '{fid}' 必须以扩展名 '{ext.name}.' 为前缀（防覆盖核心）"))
            if fid in core_flow_ids:
                issues.append(ValidationIssue("error", f"flow_id '{fid}' 与核心 flow 冲突"))
    return issues


def merge_into_registry(reg: FlowRegistry, ext: Extension) -> int:
    """把扩展的 flow 合并进注册表。只增不覆盖核心（register 会拒绝重复 id）。"""
    flows_file = ext.root / FLOWS_NAME
    if not flows_file.exists():
        return 0
    return reg.load_json(flows_file)


def has_errors(issues: List[ValidationIssue]) -> bool:
    return any(i.level == "error" for i in issues)
